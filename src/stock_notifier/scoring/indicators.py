from __future__ import annotations

import math
from collections.abc import Iterable

import pandas as pd


def _series(values: Iterable[float | int | None]) -> pd.Series:
    return pd.Series(list(values), dtype="float64")


def sma(values: Iterable[float | int | None], period: int) -> pd.Series:
    if period <= 0:
        raise ValueError("SMA period must be positive")
    return _series(values).rolling(window=period, min_periods=period).mean()


def ema(values: Iterable[float | int | None], period: int) -> pd.Series:
    if period <= 0:
        raise ValueError("EMA period must be positive")
    return _series(values).ewm(span=period, adjust=False, min_periods=period).mean()


def price_change_pct(values: Iterable[float | int | None], days: int) -> pd.Series:
    if days <= 0:
        raise ValueError("Price change lookback must be positive")
    return _series(values).pct_change(periods=days) * 100.0


def volume_ratio(volumes: Iterable[float | int | None], period: int) -> pd.Series:
    if period <= 0:
        raise ValueError("Volume ratio period must be positive")
    volume = _series(volumes)
    average = volume.rolling(window=period, min_periods=period).mean()
    return volume / average


def distance_pct(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0) or math.isnan(float(denominator)):
        return None
    return ((float(numerator) / float(denominator)) - 1.0) * 100.0


def adx(
    highs: Iterable[float | int | None],
    lows: Iterable[float | int | None],
    closes: Iterable[float | int | None],
    period: int = 14,
) -> pd.Series:
    """Compute Wilder ADX.

    This intentionally avoids TA-Lib/pandas-ta so the tiny ARM VM has no compiled dependency
    burden. It returns a pandas Series aligned with the input rows.
    """

    if period <= 0:
        raise ValueError("ADX period must be positive")

    high = _series(highs)
    low = _series(lows)
    close = _series(closes)
    if len(close) == 0:
        return pd.Series(dtype="float64")

    plus_dm: list[float] = [math.nan]
    minus_dm: list[float] = [math.nan]
    true_ranges: list[float] = [math.nan]

    for index in range(1, len(close)):
        if pd.isna(high.iloc[index]) or pd.isna(low.iloc[index]) or pd.isna(close.iloc[index - 1]):
            plus_dm.append(math.nan)
            minus_dm.append(math.nan)
            true_ranges.append(math.nan)
            continue
        up_move = float(high.iloc[index]) - float(high.iloc[index - 1])
        down_move = float(low.iloc[index - 1]) - float(low.iloc[index])
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        true_ranges.append(
            max(
                float(high.iloc[index]) - float(low.iloc[index]),
                abs(float(high.iloc[index]) - float(close.iloc[index - 1])),
                abs(float(low.iloc[index]) - float(close.iloc[index - 1])),
            )
        )

    tr = pd.Series(true_ranges, dtype="float64")
    plus = pd.Series(plus_dm, dtype="float64")
    minus = pd.Series(minus_dm, dtype="float64")

    smoothed_tr = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    smoothed_plus = plus.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    smoothed_minus = minus.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    plus_di = 100.0 * (smoothed_plus / smoothed_tr)
    minus_di = 100.0 * (smoothed_minus / smoothed_tr)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def scale_score(
    value: float | None,
    *,
    low: float,
    high: float,
    direction: str = "higher",
    neutral: float = 0.0,
) -> float:
    if value is None or pd.isna(value) or math.isclose(low, high):
        return neutral
    normalized = (float(value) - low) / (high - low)
    if direction == "lower":
        normalized = 1.0 - normalized
    return clamp(normalized * 100.0)
