from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import math
from statistics import mean, pstdev
from typing import Iterable, Sequence


@dataclass(frozen=True)
class Candle:
    open: float
    high: float
    low: float
    close: float
    base_volume: float
    quote_volume: float
    time_ms: int = 0

    @classmethod
    def from_api(cls, row: dict) -> "Candle":
        return cls(
            open=float(row.get("open") or row.get("o")),
            high=float(row.get("high") or row.get("h")),
            low=float(row.get("low") or row.get("l")),
            close=float(row.get("close") or row.get("c")),
            base_volume=float(row.get("baseVol") or row.get("b") or 0),
            quote_volume=float(row.get("quoteVol") or row.get("q") or 0),
            time_ms=int(row.get("time") or row.get("ts") or 0),
        )


def ema(values: Sequence[float], period: int) -> float:
    if period <= 0 or len(values) < period:
        raise ValueError("not enough values for EMA")
    alpha = 2.0 / (period + 1.0)
    current = sum(values[:period]) / period
    for value in values[period:]:
        current = alpha * value + (1.0 - alpha) * current
    return current


def rsi(values: Sequence[float], period: int = 14) -> float:
    if len(values) < period + 1:
        raise ValueError("not enough values for RSI")
    gains = []
    losses = []
    for a, b in zip(values[-period-1:-1], values[-period:]):
        delta = b - a
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def true_ranges(candles: Sequence[Candle]) -> list[float]:
    if len(candles) < 2:
        return []
    out: list[float] = []
    for prev, cur in zip(candles[:-1], candles[1:]):
        out.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    return out


def atr(candles: Sequence[Candle], period: int = 14) -> float:
    trs = true_ranges(candles)
    if len(trs) < period:
        raise ValueError("not enough candles for ATR")
    return sum(trs[-period:]) / period


def adx(candles: Sequence[Candle], period: int = 14) -> float:
    if len(candles) < period * 2 + 1:
        raise ValueError("not enough candles for ADX")
    trs: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for prev, cur in zip(candles[:-1], candles[1:]):
        up = cur.high - prev.high
        down = prev.low - cur.low
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    dx_values: list[float] = []
    for i in range(period - 1, len(trs)):
        tr_sum = sum(trs[i-period+1:i+1])
        if tr_sum <= 0:
            dx_values.append(0.0)
            continue
        pdi = 100.0 * sum(plus_dm[i-period+1:i+1]) / tr_sum
        mdi = 100.0 * sum(minus_dm[i-period+1:i+1]) / tr_sum
        denom = pdi + mdi
        dx_values.append(0.0 if denom <= 0 else 100.0 * abs(pdi - mdi) / denom)
    if len(dx_values) < period:
        raise ValueError("not enough DX values for ADX")
    return sum(dx_values[-period:]) / period


def vwap(candles: Sequence[Candle], period: int = 20) -> float:
    if len(candles) < period:
        raise ValueError("not enough candles for VWAP")
    selected = candles[-period:]
    total_weight = sum(max(c.base_volume, 0.0) for c in selected)
    if total_weight <= 0:
        return mean(c.close for c in selected)
    total = 0.0
    for c in selected:
        typical = (c.high + c.low + c.close) / 3.0
        total += typical * max(c.base_volume, 0.0)
    return total / total_weight


def volume_zscore(candles: Sequence[Candle], period: int = 20) -> float:
    if len(candles) < period + 1:
        raise ValueError("not enough candles for volume z-score")
    history = [c.quote_volume for c in candles[-period-1:-1]]
    current = candles[-1].quote_volume
    sigma = pstdev(history)
    if sigma == 0:
        avg = mean(history)
        if current > avg:
            return 5.0
        if current < avg:
            return -5.0
        return 0.0
    return (current - mean(history)) / sigma


def candle_position(candle: Candle) -> float:
    span = candle.high - candle.low
    if span <= 0:
        return 0.5
    return (candle.close - candle.low) / span


def body_fraction(candle: Candle) -> float:
    span = candle.high - candle.low
    if span <= 0:
        return 0.0
    return abs(candle.close - candle.open) / span


def bullish_engulfing(prev: Candle, cur: Candle) -> bool:
    return (
        prev.close < prev.open and cur.close > cur.open and
        cur.open <= prev.close and cur.close >= prev.open
    )


def bearish_engulfing(prev: Candle, cur: Candle) -> bool:
    return (
        prev.close > prev.open and cur.close < cur.open and
        cur.open >= prev.close and cur.close <= prev.open
    )


def bollinger_bands(values: Sequence[float], period: int = 20, deviations: float = 2.0) -> tuple[float, float, float]:
    if period <= 1 or len(values) < period:
        raise ValueError("not enough values for Bollinger Bands")
    selected = list(values[-period:])
    mid = mean(selected)
    sigma = pstdev(selected)
    return mid + deviations * sigma, mid, mid - deviations * sigma


def efficiency_ratio(values: Sequence[float], period: int = 20) -> float:
    """Kaufman efficiency ratio: directional change / total path length.

    Values near 1 indicate persistent directional movement; values near 0
    indicate choppy back-and-forth movement.
    """
    if period <= 1 or len(values) < period + 1:
        raise ValueError("not enough values for efficiency ratio")
    window = list(values[-(period + 1):])
    direction = abs(window[-1] - window[0])
    noise = sum(abs(b - a) for a, b in zip(window[:-1], window[1:]))
    if noise <= 0:
        return 0.0
    return direction / noise
