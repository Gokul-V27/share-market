"""BTC watcher: live or CSV-replayed candles -> Laya's rule-based read.

Setups are built ONLY from levels the user supplies (--level / --levels-file).
Without levels the watcher still scores trend, structure and momentum, but it
proposes no trades and asks for the levels.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .engine import Result, analyse
from .indicators import atr, nearest_levels, read_timeframe
from .market_data import TF_MS, Candle, fetch_candles, resample
from .models import AnalysisInput, Level, Setup, fmt_price
from .report import render

TIMEFRAMES = ("1h", "15m", "5m", "1m")
BUFFER_ATR = 0.5  # entry/stop sit half an exec-timeframe ATR either side of the level
STALE_EXEC_CANDLES = 8  # paper trades still open after 8 exec candles are closed (levels stale)


def load_levels(path: Optional[str], extra: list[float]) -> list[Level]:
    levels = []
    if path:
        with open(path) as fh:
            data = json.load(fh)
        rows = data.get("levels", data) if isinstance(data, dict) else data
        levels.extend(Level.from_dict(r) for r in rows)
    levels.extend(Level(price=p, label="cli") for p in extra)
    uniq = {lv.price: lv for lv in levels}
    return [uniq[p] for p in sorted(uniq)]


def build_setups(price: float, levels: list[Level], atr_exec: Optional[float], exec_tf: str) -> list[Setup]:
    """Up to four close-triggered setups around the nearest drawn support and resistance."""
    if not levels or not atr_exec:
        return []
    buf = BUFFER_ATR * atr_exec
    prices = sorted(lv.price for lv in levels)
    support, resistance = nearest_levels(price, levels)
    above = lambda x: [p for p in prices if p > x]  # noqa: E731
    below = lambda x: [p for p in prices if p < x][::-1]  # noqa: E731
    setups = []

    def add(**kw):
        setups.append(Setup(id=len(setups) + 1, timeframe=exec_tf, **kw))

    if resistance is not None:
        nxt = above(resistance)
        if nxt:
            add(kind="breakout", trigger_side="above", trigger_level=resistance, entry=resistance + buf,
                stop=resistance - buf, target1=nxt[0], target2=nxt[1] if len(nxt) > 1 else None,
                rationale=f"Breakout of drawn resistance {fmt_price(resistance)}")
        if support is not None:
            add(kind="rejection", trigger_side="below", trigger_level=resistance, entry=resistance - buf,
                stop=resistance + buf, target1=support, target2=(below(support) or [None])[0],
                trigger_note="after a wick above", rationale=f"Rejection at drawn resistance {fmt_price(resistance)}")
    if support is not None:
        nxt = below(support)
        if nxt:
            add(kind="breakdown", trigger_side="below", trigger_level=support, entry=support - buf,
                stop=support + buf, target1=nxt[0], target2=nxt[1] if len(nxt) > 1 else None,
                rationale=f"Breakdown of drawn support {fmt_price(support)}")
        if resistance is not None:
            add(kind="bounce", trigger_side="above", trigger_level=support, entry=support + buf,
                stop=support - buf, target1=resistance, target2=(above(resistance) or [None])[0],
                trigger_note="after a wick below", rationale=f"Bounce off drawn support {fmt_price(support)}")
    return setups


def trigger_fired(setup: Setup, prev: Candle, cur: Candle) -> bool:
    """Did the just-closed candle `cur` satisfy the setup's close-only trigger?"""
    lv = setup.trigger_level
    if setup.kind == "breakout":
        return prev.close <= lv < cur.close
    if setup.kind == "breakdown":
        return prev.close >= lv > cur.close
    if setup.kind == "bounce":
        return cur.low < lv < cur.close
    if setup.kind == "rejection":
        return cur.high > lv > cur.close
    return False


def level_events(prev: Candle, cur: Candle, levels: list[Level]) -> list[str]:
    events = []
    for lv in levels:
        p = lv.price
        if prev.close <= p < cur.close:
            events.append(f"closed above {fmt_price(p)}")
        elif prev.close >= p > cur.close:
            events.append(f"closed below {fmt_price(p)}")
        elif cur.low < p < cur.close:
            events.append(f"wicked below {fmt_price(p)} and closed back above")
        elif cur.high > p > cur.close:
            events.append(f"wicked above {fmt_price(p)} and closed back below")
    return events


def build_analysis(symbol: str, candles: dict[str, list[Candle]], levels: list[Level], now_ms: int,
                   exec_tf: str = "5m", cost_pct: float = 0.10, source: str = "") -> AnalysisInput:
    reads = [read_timeframe(tf, candles[tf], levels, now_ms) for tf in TIMEFRAMES if candles.get(tf)]
    exec_closed = [c for c in candles.get(exec_tf, []) if c.is_closed(now_ms)]
    setups = []
    if exec_closed:
        setups = build_setups(exec_closed[-1].close, levels, atr(exec_closed), exec_tf)
    return AnalysisInput(
        symbol=symbol,
        timeframes=reads,
        levels=levels,
        setups=setups,
        exec_timeframe=exec_tf,
        round_trip_cost_pct=cost_pct,
        has_volume=True,
        source=source,
        analysed_at=datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        blind_spots=[
            "Volume is one venue's prints, not the whole market",
            "Your trendlines can't be read from price data; only the horizontal levels you supplied are used",
            "4h/daily context not scored",
        ],
    )


def snapshot(candles_1m: list[Candle], now_ms: int) -> dict[str, list[Candle]]:
    """What each timeframe's chart looked like at now_ms, built from 1m candles."""
    return {tf: resample(candles_1m, tf, now_ms)[-150:] for tf in TIMEFRAMES}


# --- paper-trade replay -----------------------------------------------------------------------

@dataclass
class Trade:
    opened: int
    setup: Setup
    fill: float
    exit: Optional[float] = None
    closed: Optional[int] = None
    reason: str = ""

    @property
    def risk(self) -> float:
        return abs(self.fill - self.setup.stop)

    def r_multiple(self, cost_pct: float = 0.0) -> float:
        if self.exit is None or not self.risk:
            return 0.0
        sign = 1 if self.setup.direction == "long" else -1
        gross = sign * (self.exit - self.fill)
        return (gross - self.fill * cost_pct / 100) / self.risk


def _ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def replay(candles_1m: list[Candle], levels: list[Level], symbol: str = "BTCUSD", exec_tf: str = "5m",
           cost_pct: float = 0.10, warmup_minutes: Optional[int] = None) -> tuple[list[Trade], list[str]]:
    """Step through history one exec candle at a time and paper-trade Laya's own rules.

    A trade is taken only when the rules would say ENTER-ON-TRIGGER: a valid, with-trend,
    not-weak setup (as built at the previous close) whose close-only trigger fires. Fill is the
    trigger candle's close. Exit at stop, target 1, or after 8 exec candles. When stop and target
    sit inside the same 1m candle, the stop is assumed hit first.
    """
    step = TF_MS[exec_tf]
    if not candles_1m:
        return [], []
    if warmup_minutes is None:
        warmup_minutes = 27 * step // 60_000  # enough exec candles for MA26
    start = candles_1m[0].open_time + warmup_minutes * 60_000
    start += (-start) % step
    end = candles_1m[-1].close_time
    by_time = {c.open_time: c for c in candles_1m}
    trades: list[Trade] = []
    log: list[str] = []
    pending: Optional[Result] = None
    open_trade: Optional[Trade] = None
    now = start
    while now <= end:
        snap = snapshot(candles_1m, now)
        exec_c = [c for c in snap[exec_tf] if c.is_closed(now)]
        if open_trade is not None:
            _manage(open_trade, by_time, now - step, now, step)
            if open_trade.exit is not None:
                trades.append(open_trade)
                log.append(_trade_line(open_trade, cost_pct))
                open_trade = None
        if open_trade is None and pending is not None and len(exec_c) >= 2:
            prev, cur = exec_c[-2], exec_c[-1]
            v = pending.verdict
            for r in pending.setups:
                if (v.bias != "mixed" and r.valid and r.alignment == "with-trend" and not r.weak
                        and trigger_fired(r.setup, prev, cur)):
                    open_trade = Trade(opened=now, setup=r.setup, fill=cur.close)
                    if (r.direction == "long" and cur.close <= r.setup.stop) or (
                            r.direction == "short" and cur.close >= r.setup.stop):
                        open_trade = None  # the close is already through the stop; no trade
                        continue
                    break
        pending = analyse(build_analysis(symbol, snap, levels, now, exec_tf, cost_pct, "csv replay"))
        now += step
    if open_trade is not None:
        last = candles_1m[-1]
        open_trade.exit, open_trade.closed, open_trade.reason = last.close, last.close_time, "end of data"
        trades.append(open_trade)
        log.append(_trade_line(open_trade, cost_pct))
    return trades, log


def _manage(t: Trade, by_time: dict[int, Candle], frm: int, to: int, step: int) -> None:
    long = t.setup.direction == "long"
    ms = frm
    while ms < to:
        c = by_time.get(ms)
        ms += 60_000
        if c is None or c.open_time < t.opened:
            continue
        hit_stop = c.low <= t.setup.stop if long else c.high >= t.setup.stop
        hit_t1 = c.high >= t.setup.target1 if long else c.low <= t.setup.target1
        if hit_stop:
            t.exit, t.closed, t.reason = t.setup.stop, c.close_time, "stop"
            return
        if hit_t1:
            t.exit, t.closed, t.reason = t.setup.target1, c.close_time, "target 1"
            return
    if to - t.opened >= STALE_EXEC_CANDLES * step:
        last = by_time.get(to - 60_000)
        if last is not None:
            t.exit, t.closed, t.reason = last.close, to, f"time stop ({STALE_EXEC_CANDLES} candles)"


def _trade_line(t: Trade, cost_pct: float) -> str:
    s = t.setup
    return (f"{_ts(t.opened)} {s.direction:5} {s.kind:9} fill {fmt_price(t.fill)} stop {fmt_price(s.stop)} "
            f"T1 {fmt_price(s.target1)} -> {fmt_price(t.exit)} ({t.reason}) "
            f"{t.r_multiple():+.2f}R gross, {t.r_multiple(cost_pct):+.2f}R net")


def summarize(trades: list[Trade], cost_pct: float) -> str:
    if not trades:
        return "No trades: the rules never produced a with-trend, non-weak setup that triggered."
    gross = [t.r_multiple() for t in trades]
    net = [t.r_multiple(cost_pct) for t in trades]
    wins = sum(1 for r in net if r > 0)
    return (f"{len(trades)} paper trades | {wins} net winners | total {sum(gross):+.2f}R gross, "
            f"{sum(net):+.2f}R net after {cost_pct:.2f}% round trip | avg {sum(net) / len(net):+.2f}R net")


def account(trades: list[Trade], balance: float, cost_pct: float, leverage: float = 1.0) -> list[str]:
    """Run the trades through a small paper account, compounding.

    Each trade puts the whole balance x leverage into the position (a $10 account is too small
    to size by risk). P&L = notional x price move % - notional x round-trip cost %.
    Shorts assume a venue that allows them (futures/margin).
    """
    lines = [f"Paper account: start ${balance:,.2f}, full balance per trade, {leverage:g}x leverage, "
             f"{cost_pct:.2f}% round-trip cost"]
    start, peak, worst_dd = balance, balance, 0.0
    for t in trades:
        if balance <= 0:
            lines.append("account wiped out; no further trades")
            break
        sign = 1 if t.setup.direction == "long" else -1
        notional = balance * leverage
        pnl = notional * (sign * (t.exit - t.fill) / t.fill - cost_pct / 100)
        balance = max(0.0, balance + pnl)
        peak = max(peak, balance)
        worst_dd = max(worst_dd, (peak - balance) / peak * 100 if peak else 0)
        lines.append(f"{_ts(t.opened)} {t.setup.direction:5} {t.setup.kind:9} {t.reason:22} "
                     f"P&L {pnl:+.4f} USD -> balance ${balance:,.4f}")
    change = balance - start
    lines.append(f"End balance ${balance:,.4f} ({change:+.4f} USD, {change / start * 100:+.2f}%), "
                 f"worst drawdown {worst_dd:.2f}%")
    return lines


# --- live --------------------------------------------------------------------------------------

def fetch_all(symbol: str, source: str) -> tuple[dict[str, list[Candle]], str]:
    out, name = {}, ""
    for tf in TIMEFRAMES:
        out[tf], name = fetch_candles(symbol, tf, limit=150, source=source)
    return out, name


def watch(symbol: str, levels: list[Level], exec_tf: str, cost_pct: float, source: str,
          every: float, once: bool, as_json: bool) -> int:
    from .report import render_json

    last_alert: Optional[int] = None
    while True:
        try:
            candles, name = fetch_all(symbol, source)
        except Exception as e:  # network trouble shouldn't kill a long-running watch
            print(f"[data error] {e}", file=sys.stderr)
            if once:
                return 1
            time.sleep(every)
            continue
        now = int(time.time() * 1000)
        res = analyse(build_analysis(symbol, candles, levels, now, exec_tf, cost_pct, name))
        if not once:
            print("\033[2J\033[H", end="")
        print(render_json(res) if as_json else render(res))
        exec_c = [c for c in candles[exec_tf] if c.is_closed(now)]
        if len(exec_c) >= 2 and exec_c[-1].open_time != last_alert:
            prev, cur = exec_c[-2], exec_c[-1]
            fired = [s for s in res.inp.setups if trigger_fired(s, prev, cur)]
            events = level_events(prev, cur, levels)
            if fired or events:
                last_alert = cur.open_time
                print("\a")
                for e in events:
                    print(f"[{exec_tf} close {_ts(cur.open_time)}] {e}")
                for s in fired:
                    print(f"[{exec_tf} close {_ts(cur.open_time)}] TRIGGER (study): setup {s.id} {s.kind} "
                          f"{s.trigger_text}")
        if once:
            return 0
        time.sleep(every)
