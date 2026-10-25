"""Two rule-based day-trading bots, adapted to 24/7 BTC, for side-by-side paper tests.

* ORB  - opening range breakout: range = first N minutes after 00:00 UTC; a 5m close beyond it
         enters, stop at the other side of the range, exit at the day's end (one trade a day).
* LVL  - liquidity sweep: range = the Asia session (00:00-08:00 UTC). During 08:00-20:00 UTC, once
         price trades beyond one side and a 5m candle closes back inside, buy (sell) when price
         reaches 25% of the range from the swept side; stop at 12.5%, target at 50% (1:2).
         Exit at 20:00 UTC if neither is hit. One trade a day.

A news/insider-filing bot is not included: BTC has no insider filings and no historical
headline feed is available here, so it can't be tested honestly.

Trades come back as btc_watcher.Trade objects so the same $ account maths applies.
"""

from __future__ import annotations

from typing import Optional

from .btc_watcher import Trade
from .market_data import TF_MS, Candle, resample
from .models import Setup

DAY_MS = 86_400_000
HOUR_MS = 3_600_000


def _days(candles_1m: list[Candle]) -> dict[int, list[Candle]]:
    days: dict[int, list[Candle]] = {}
    for c in candles_1m:
        days.setdefault(c.open_time - c.open_time % DAY_MS, []).append(c)
    return days


def _run_exit(t: Trade, bars: list[Candle], end_ms: int, filled_inside_first: bool = False) -> None:
    """Walk 1m bars after entry: stop first, then target, else exit at end_ms's last close.

    filled_inside_first: the entry happened inside the first bar, so its high/low may predate the
    fill; for that bar only its close counts.
    """
    long = t.setup.direction == "long"
    last = None
    for c in bars:
        if c.open_time < t.opened:
            continue
        if c.open_time >= end_ms:
            break
        last = c
        if filled_inside_first and c.open_time == t.opened:
            lo = hi = c.close
        else:
            lo, hi = c.low, c.high
        if (lo <= t.stop) if long else (hi >= t.stop):
            t.exit, t.closed, t.reason = t.stop, c.close_time, "stop"
            return
        if t.target and ((hi >= t.target) if long else (lo <= t.target)):
            t.exit, t.closed, t.reason = t.target, c.close_time, "target"
            return
    if last is not None:
        t.exit, t.closed, t.reason = last.close, last.close_time, "session end"


def _trade(kind: str, direction: str, when: int, fill: float, stop: float, target: Optional[float],
           level: float) -> Trade:
    side = "above" if direction == "long" else "below"
    setup = Setup(id=1, trigger_side=side, trigger_level=level, entry=fill, stop=stop,
                  target1=target if target else fill, timeframe="5m", kind=kind)
    risk = abs(fill - stop)
    return Trade(opened=when, setup=setup, fill=fill, target=target or 0.0, stop=stop,
                 planned_r=abs(target - fill) / risk if target and risk else 0.0)


def orb_bot(candles_1m: list[Candle], range_minutes: int = 30, target_r: Optional[float] = None) -> list[Trade]:
    trades = []
    for day, bars in sorted(_days(candles_1m).items()):
        or_end = day + range_minutes * 60_000
        opening = [c for c in bars if c.open_time < or_end]
        if len(opening) < range_minutes * 0.9:
            continue  # incomplete opening range
        hi, lo = max(c.high for c in opening), min(c.low for c in opening)
        if hi <= lo:
            continue
        after = [c for c in bars if c.open_time >= or_end]
        for c5 in resample(after, "5m"):
            if c5.close > hi or c5.close < lo:
                long = c5.close > hi
                fill, stop = c5.close, (lo if long else hi)
                target = fill + (1 if long else -1) * target_r * abs(fill - stop) if target_r else None
                t = _trade("orb", "long" if long else "short", c5.close_time, fill, stop, target,
                           hi if long else lo)
                _run_exit(t, after, day + DAY_MS)
                if t.exit is not None:
                    trades.append(t)
                break
    return trades


def sweep_bot(candles_1m: list[Candle], range_hours: tuple[int, int] = (0, 8), trade_until_hour: int = 20,
              entry_frac: float = 0.25, stop_frac: float = 0.125, target_frac: float = 0.5) -> list[Trade]:
    trades = []
    for day, bars in sorted(_days(candles_1m).items()):
        r0, r1 = day + range_hours[0] * HOUR_MS, day + range_hours[1] * HOUR_MS
        end = day + trade_until_hour * HOUR_MS
        rng = [c for c in bars if r0 <= c.open_time < r1]
        if len(rng) < (r1 - r0) / 60_000 * 0.9:
            continue
        hi, lo = max(c.high for c in rng), min(c.low for c in rng)
        size = hi - lo
        if size <= 0:
            continue
        window = [c for c in bars if r1 <= c.open_time < end]
        swept_low = swept_high = False
        armed: Optional[tuple[str, int]] = None
        for c5 in resample(window, "5m"):
            swept_low |= c5.low < lo
            swept_high |= c5.high > hi
            if swept_low and lo < c5.close < hi:
                armed = ("long", c5.close_time)
            elif swept_high and lo < c5.close < hi:
                armed = ("short", c5.close_time)
            if armed:
                break
        if not armed:
            continue
        direction, armed_at = armed
        sign = 1 if direction == "long" else -1
        base = lo if direction == "long" else hi
        entry, stop, target = (base + sign * f * size for f in (entry_frac, stop_frac, target_frac))
        t = None
        for c in window:
            if c.open_time < armed_at:
                continue
            if (c.close >= target) if direction == "long" else (c.close <= target):
                break  # ran to the target before an entry; no trade
            touched = (c.high >= entry) if direction == "long" else (c.low <= entry)
            if touched:
                fill = max(entry, c.open) if direction == "long" else min(entry, c.open)  # stop order
                if sign * (target - fill) <= 0 or sign * (fill - stop) <= 0:
                    break
                t = _trade("sweep", direction, c.open_time, fill, stop, target, base)
                break
        if t is not None:
            _run_exit(t, [c for c in window if c.open_time >= t.opened], end, filled_inside_first=True)
            if t.exit is not None:
                trades.append(t)
    return trades


def equity(trades: list[Trade], balance: float, cost_pct: float) -> tuple[float, float]:
    """(end balance, worst drawdown %) with the whole balance in each trade, compounding."""
    peak, dd = balance, 0.0
    for t in trades:
        sign = 1 if t.setup.direction == "long" else -1
        balance = max(0.0, balance * (1 + sign * (t.exit - t.fill) / t.fill - cost_pct / 100))
        peak = max(peak, balance)
        dd = max(dd, (peak - balance) / peak * 100 if peak else 0.0)
    return balance, dd
