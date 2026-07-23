from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from stock_notifier.scoring.indicators import (
    adx,
    distance_pct,
    ema,
    price_change_pct,
    scale_score,
    sma,
    volume_ratio,
)


@dataclass(frozen=True)
class SignalDefinition:
    name: str
    config: dict[str, Any]
    signal_id: int | None = None
    enabled: bool = True


@dataclass(frozen=True)
class ComponentResult:
    name: str
    component_type: str
    mode: str
    value: float | None
    passed: bool
    score: float
    weight: float
    contribution: float
    message: str


@dataclass(frozen=True)
class ScoredSymbol:
    symbol: str
    signal_name: str
    trading_date: str | None
    close: float | None
    score: float
    eligible: bool
    components: list[ComponentResult]
    message: str


def _as_float(value: Any) -> float | None:
    if value in (None, "", "N/A"):
        return None
    try:
        candidate = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(candidate):
        return None
    return candidate


def _operator_passes(value: float | None, operator: str, threshold: float | None) -> bool:
    if value is None or threshold is None:
        return False
    if operator == ">":
        return value > threshold
    if operator == ">=":
        return value >= threshold
    if operator == "<":
        return value < threshold
    if operator == "<=":
        return value <= threshold
    if operator in {"=", "=="}:
        return math.isclose(value, threshold)
    raise ValueError(f"Unsupported operator: {operator}")


def _latest(series: pd.Series, index: int) -> float | None:
    if index < 0 or index >= len(series):
        return None
    return _as_float(series.iloc[index])


def _indicator_value(frame: pd.DataFrame, component: dict[str, Any]) -> tuple[float | None, str]:
    component_type = str(component.get("type") or "").strip()
    params = dict(component.get("params") or {})
    index = len(frame) - 1
    if frame.empty or index < 0:
        return None, "No price history"

    close = frame["close"]
    latest_close = _as_float(close.iloc[index])

    if component_type in {"price_vs_sma", "ma_distance_pct"}:
        period = int(params.get("period") or component.get("period") or 20)
        ma_value = _latest(sma(close, period), index)
        value = distance_pct(latest_close, ma_value)
        return value, f"Close vs SMA{period}"

    if component_type == "price_vs_ema":
        period = int(params.get("period") or component.get("period") or 20)
        ma_value = _latest(ema(close, period), index)
        value = distance_pct(latest_close, ma_value)
        return value, f"Close vs EMA{period}"

    if component_type == "sma_crossover":
        fast = int(params.get("fast_period") or component.get("fast_period") or 5)
        slow = int(params.get("slow_period") or component.get("slow_period") or 20)
        fast_value = _latest(sma(close, fast), index)
        slow_value = _latest(sma(close, slow), index)
        return distance_pct(fast_value, slow_value), f"SMA{fast} vs SMA{slow}"

    if component_type == "ema_crossover":
        fast = int(params.get("fast_period") or component.get("fast_period") or 5)
        slow = int(params.get("slow_period") or component.get("slow_period") or 20)
        fast_value = _latest(ema(close, fast), index)
        slow_value = _latest(ema(close, slow), index)
        return distance_pct(fast_value, slow_value), f"EMA{fast} vs EMA{slow}"

    if component_type == "adx":
        period = int(params.get("period") or component.get("period") or 14)
        value = _latest(adx(frame["high"], frame["low"], close, period), index)
        return value, f"ADX{period}"

    if component_type == "volume_ratio":
        period = int(params.get("period") or component.get("period") or 20)
        value = _latest(volume_ratio(frame["volume"], period), index)
        return value, f"Volume / {period}-day average"

    if component_type == "latest_volume":
        value = _as_float(frame["volume"].iloc[index])
        return value, "Latest volume"

    if component_type == "dollar_volume":
        latest_volume = _as_float(frame["volume"].iloc[index])
        value = latest_close * latest_volume if latest_close is not None and latest_volume is not None else None
        return value, "Dollar volume"

    if component_type == "price_change_pct":
        days = int(params.get("days") or component.get("days") or 5)
        value = _latest(price_change_pct(close, days), index)
        return value, f"{days}-day price change %"

    raise ValueError(f"Unsupported component type: {component_type}")


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        if len(text) <= 10:
            return datetime.fromisoformat(text[:10]).replace(tzinfo=UTC)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _price_target_metric(
    frame: pd.DataFrame,
    component: dict[str, Any],
    targets: list[dict[str, Any]],
) -> tuple[float | None, str]:
    params = dict(component.get("params") or {})
    metric = str(params.get("metric") or "target_score")
    index = len(frame) - 1
    if frame.empty or index < 0:
        return None, "Price targets: no price history"
    current_price = _as_float(frame["close"].iloc[index])
    if current_price in (None, 0):
        return None, "Price targets: no current price"

    max_targets_for_score = max(_as_float(params.get("max_targets_for_score")) or 5.0, 1.0)
    max_upside_pct_for_score = max(_as_float(params.get("max_upside_pct_for_score")) or 25.0, 0.01)
    half_life_days = max(_as_float(params.get("recency_half_life_days")) or 90.0, 1.0)
    count_weight = max(_as_float(params.get("count_weight")) or 0.4, 0.0)
    upside_weight = max(_as_float(params.get("upside_weight")) or 0.4, 0.0)
    recency_weight = max(_as_float(params.get("recency_weight")) or 0.2, 0.0)
    weight_total = count_weight + upside_weight + recency_weight
    if weight_total <= 0:
        count_weight, upside_weight, recency_weight, weight_total = 0.4, 0.4, 0.2, 1.0

    now_dt = datetime.now(UTC)
    valid: list[dict[str, float]] = []
    for row in targets:
        target_price = _as_float(row.get("target_price"))
        if target_price is None or target_price <= current_price:
            continue
        if str(row.get("reached_date") or "").strip():
            continue
        upside_pct = (target_price - current_price) / current_price * 100.0
        reference_date = _parse_date(row.get("effective_date")) or _parse_date(row.get("captured_at"))
        age_days = max((now_dt - reference_date).days, 0) if reference_date else half_life_days
        recency_score = 100.0 * (0.5 ** (age_days / half_life_days))
        valid.append({"upside_pct": upside_pct, "recency_score": recency_score})

    unreached_count = float(len(valid))
    avg_upside_pct = (
        sum(item["upside_pct"] for item in valid) / len(valid)
        if valid
        else 0.0
    )
    avg_recency_score = (
        sum(item["recency_score"] for item in valid) / len(valid)
        if valid
        else 0.0
    )
    count_score = min(unreached_count / max_targets_for_score, 1.0) * 100.0
    upside_score = min(max(avg_upside_pct, 0.0) / max_upside_pct_for_score, 1.0) * 100.0
    target_score = (
        count_score * count_weight
        + upside_score * upside_weight
        + avg_recency_score * recency_weight
    ) / weight_total

    metric_values = {
        "target_score": target_score,
        "unreached_count": unreached_count,
        "avg_upside_pct": avg_upside_pct,
        "recency_score": avg_recency_score,
    }
    value = metric_values.get(metric, target_score)
    label = (
        "Price target strength"
        if metric == "target_score"
        else "Unreached price targets"
        if metric == "unreached_count"
        else "Average price-target upside %"
        if metric == "avg_upside_pct"
        else "Price-target recency score"
    )
    return value, (
        f"{label} (unreached={int(unreached_count)}, "
        f"avg upside={avg_upside_pct:.2f}%, recency={avg_recency_score:.1f})"
    )


def _component_result(
    frame: pd.DataFrame,
    component: dict[str, Any],
    targets: list[dict[str, Any]] | None = None,
) -> ComponentResult:
    component_type = str(component.get("type") or "").strip()
    mode = str(component.get("mode") or "score").strip().lower()
    if mode not in {"score", "gate"}:
        raise ValueError(f"Unsupported component mode: {mode}")
    name = str(component.get("name") or component_type or "Component")
    if component_type == "price_target":
        value, label = _price_target_metric(frame, component, targets or [])
    else:
        value, label = _indicator_value(frame, component)

    operator = str(component.get("operator") or ">=").strip()
    threshold = _as_float(component.get("threshold"))
    passed = _operator_passes(value, operator, threshold) if threshold is not None else value is not None

    weight = _as_float(component.get("weight")) or 0.0
    if mode == "gate":
        # Gates are filters, not ranking inputs. Keep score/weight/contribution
        # at zero so component breakdowns cannot be mistaken for weighted score
        # components.
        score = 0.0
        weight = 0.0
        contribution = 0.0
    else:
        score_min = _as_float(component.get("score_min"))
        score_max = _as_float(component.get("score_max"))
        if score_min is None:
            score_min = threshold if threshold is not None else 0.0
        if score_max is None:
            score_max = score_min + 10.0
        score = scale_score(
            value,
            low=score_min,
            high=score_max,
            direction=str(component.get("direction") or "higher"),
        )
        contribution = score * weight

    if value is None:
        message = f"{label}: insufficient data"
    elif threshold is None:
        message = f"{label}: {value:.2f}"
    else:
        message = f"{label}: {value:.2f} {operator} {threshold:g} = {'yes' if passed else 'no'}"

    return ComponentResult(
        name=name,
        component_type=component_type,
        mode=mode,
        value=value,
        passed=passed,
        score=round(score, 4),
        weight=round(weight, 4),
        contribution=round(contribution, 4),
        message=message,
    )


def evaluate_signal(
    definition: SignalDefinition,
    history_by_symbol: dict[str, pd.DataFrame],
    price_targets_by_symbol: dict[str, list[dict[str, Any]]] | None = None,
) -> list[ScoredSymbol]:
    config = dict(definition.config or {})
    components = list(config.get("components") or [])
    if not components:
        raise ValueError(f"Signal '{definition.name}' has no components")

    results: list[ScoredSymbol] = []
    for symbol, frame in sorted(history_by_symbol.items()):
        prepared = frame.sort_values("trading_date").reset_index(drop=True).copy()
        symbol_targets = (price_targets_by_symbol or {}).get(symbol, [])
        component_results = [_component_result(prepared, component, symbol_targets) for component in components]
        failed_gates = [
            component for component in component_results if component.mode == "gate" and not component.passed
        ]
        score_components = [component for component in component_results if component.mode == "score"]
        weight_sum = sum(component.weight for component in score_components if component.weight > 0)
        raw_score = (
            sum(component.contribution for component in score_components) / weight_sum
            if weight_sum > 0
            else 0.0
        )
        eligible = not failed_gates
        score = raw_score if eligible else 0.0
        latest = prepared.iloc[-1] if not prepared.empty else {}
        message = "OK" if eligible else "Failed gates: " + ", ".join(component.name for component in failed_gates)
        if not score_components:
            message = f"{message}; no weighted components"
        results.append(
            ScoredSymbol(
                symbol=symbol,
                signal_name=definition.name,
                trading_date=str(latest.get("trading_date")) if len(prepared) else None,
                close=_as_float(latest.get("close")) if len(prepared) else None,
                score=round(score, 2),
                eligible=eligible,
                components=component_results,
                message=message,
            )
        )

    results.sort(key=lambda item: (item.eligible, item.score), reverse=True)
    return results
