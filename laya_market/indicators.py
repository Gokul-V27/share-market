"""Score a timeframe from candles, using the same four components as the chart read."""

from __future__ import annotations

from typing import Optional

from .market_data import Candle
from .models import Level, TimeframeRead, fmt_price

MA_FAST, MA_SLOW = 9, 26
EQUAL_TOL = 0.0005  # swings within 0.05% count as equal


def sma(values: list[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def atr(candles: list[Candle], n: int = 14) -> Optional[float]:
    if len(candles) < 2:
        return None
    trs = []
    for prev, c in zip(candles, candles[1:]):
        trs.append(max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close)))
    trs = trs[-n:]
    return sum(trs) / len(trs)


def pivots(candles: list[Candle], side: int = 2) -> tuple[list[float], list[float]]:
    """Swing highs and lows: a bar whose high (low) beats `side` bars on each side."""
    highs, lows = [], []
    for i in range(side, len(candles) - side):
        window = candles[i - side : i + side + 1]
        c = candles[i]
        if c.high == max(w.high for w in window) and all(w.high < c.high for w in window if w is not c):
            highs.append(c.high)
        if c.low == min(w.low for w in window) and all(w.low > c.low for w in window if w is not c):
            lows.append(c.low)
    return highs, lows


def _compare(a: float, b: float) -> int:
    if abs(b - a) <= a * EQUAL_TOL:
        return 0
    return 1 if b > a else -1


def score_trend(price: float, fast: Optional[float], slow: Optional[float]) -> int:
    if fast is None or slow is None:
        return 0
    if price > fast and price > slow:
        return 2 if fast > slow else 1
    if price < fast and price < slow:
        return -2 if fast < slow else -1
    return 0


def score_structure(candles: list[Candle]) -> int:
    highs, lows = pivots(candles)
    score = 0
    if len(highs) >= 2:
        score += _compare(highs[-2], highs[-1])
    if len(lows) >= 2:
        score += _compare(lows[-2], lows[-1])
    return score


def score_momentum(candles: list[Candle], atr_value: Optional[float], bars: int = 3) -> int:
    if not atr_value or len(candles) < bars:
        return 0
    net = sum(c.close - c.open for c in candles[-bars:]) / atr_value
    if net >= 1.5:
        return 2
    if net >= 0.5:
        return 1
    if net <= -1.5:
        return -2
    if net <= -0.5:
        return -1
    return 0


def nearest_levels(price: float, levels: list[Level]) -> tuple[Optional[float], Optional[float]]:
    below = [lv.price for lv in levels if lv.price < price]
    above = [lv.price for lv in levels if lv.price > price]
    return (max(below) if below else None, min(above) if above else None)


def score_location(price: float, levels: list[Level], atr_value: Optional[float]) -> int:
    """+1 sitting just above support with room overhead, -1 pressed under resistance."""
    if not levels or not atr_value:
        return 0
    support, resistance = nearest_levels(price, levels)
    near_s = support is not None and (price - support) <= atr_value
    near_r = resistance is not None and (resistance - price) <= atr_value
    if near_s and not near_r:
        return 1
    if near_r and not near_s:
        return -1
    return 0


def read_timeframe(tf: str, candles: list[Candle], levels: list[Level], now_ms: int) -> TimeframeRead:
    """Score one timeframe. Only CLOSED candles are scored; the forming one gives last price and countdown."""
    closed = [c for c in candles if c.is_closed(now_ms)]
    forming = candles[-1] if candles and not candles[-1].is_closed(now_ms) else None
    notes = []
    if len(closed) < MA_SLOW + 1:
        notes.append(f"only {len(closed)} closed candles; MA{MA_SLOW} needs {MA_SLOW}")
    if not closed:
        return TimeframeRead(timeframe=tf, notes=notes)
    step = closed[-1].interval_ms
    closes = [c.close for c in closed]
    ref = closed[-1].close
    fast, slow = sma(closes, MA_FAST), sma(closes, MA_SLOW)
    a = atr(closed)
    if not levels:
        notes.append("location unscored: no drawn levels")
    vols = [c.volume for c in closed[-21:-1]]
    if vols and sum(vols) > 0:
        notes.append(f"vol {closed[-1].volume / (sum(vols) / len(vols)):.1f}x 20-bar avg")
    if a:
        notes.append(f"ATR14 {fmt_price(a)}")
    return TimeframeRead(
        timeframe=tf,
        trend=score_trend(ref, fast, slow),
        structure=score_structure(closed[-60:]),
        momentum=score_momentum(closed, a),
        location=score_location(ref, levels, a),
        last_price=forming.close if forming else ref,
        ma_fast=fast,
        ma_slow=slow,
        seconds_to_close=(forming.close_time - now_ms if forming else step - now_ms % step) / 1000,
        notes=notes,
    )
