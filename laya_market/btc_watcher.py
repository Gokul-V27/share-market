"""BTC watcher: live or CSV-replayed candles -> Laya's rule-based read.

Setups are built ONLY from levels the user supplies (--level / --levels-file).
Without levels the watcher still scores trend, structure and momentum, but it
proposes no trades and asks for the levels.
"""

from __future__ import annotations

import json
import sys
import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .engine import WEAK_RR, Result, analyse
from .indicators import atr, nearest_levels, read_timeframe
from .market_data import TF_MS, Candle, fetch_candles, resample
from .models import AnalysisInput, Level, Setup, fmt_price
from .report import render

TIMEFRAMES = ("1h", "15m", "5m", "1m")
BUFFER_ATR = 0.5  # entry/stop sit half an exec-timeframe ATR either side of the level
SNAPSHOT_BARS = 150
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


def build_setups(price: float, levels: list[Level], atr_exec: Optional[float], exec_tf: str,
                 buffer_atr: float = BUFFER_ATR) -> list[Setup]:
    """Up to four close-triggered setups around the nearest drawn support and resistance."""
    if not levels or not atr_exec:
        return []
    buf = buffer_atr * atr_exec
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
                   exec_tf: str = "5m", cost_pct: float = 0.10, source: str = "",
                   buffer_atr: float = BUFFER_ATR) -> AnalysisInput:
    reads = [read_timeframe(tf, candles[tf], levels, now_ms) for tf in TIMEFRAMES if candles.get(tf)]
    exec_closed = [c for c in candles.get(exec_tf, []) if c.is_closed(now_ms)]
    setups = []
    if exec_closed:
        setups = build_setups(exec_closed[-1].close, levels, atr(exec_closed), exec_tf, buffer_atr)
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
    return {tf: resample(candles_1m, tf, now_ms)[-SNAPSHOT_BARS:] for tf in TIMEFRAMES}


class Snapshotter:
    """snapshot() for many moments in a long history: resample once, then slice."""

    def __init__(self, candles_1m: list[Candle]):
        self.m = candles_1m
        self.m_opens = [c.open_time for c in candles_1m]
        self.full = {tf: resample(candles_1m, tf) for tf in TIMEFRAMES}
        self.opens = {tf: [c.open_time for c in self.full[tf]] for tf in TIMEFRAMES}

    def at(self, now_ms: int) -> dict[str, list[Candle]]:
        out = {}
        last_closed_1m = bisect_right(self.m_opens, now_ms - TF_MS["1m"])
        for tf in TIMEFRAMES:
            bucket = now_ms - now_ms % TF_MS[tf]
            j = bisect_left(self.opens[tf], bucket)
            bars = self.full[tf][max(0, j - SNAPSHOT_BARS):j]
            i0 = bisect_left(self.m_opens, bucket)
            if last_closed_1m > i0:  # the forming candle, from the 1m candles closed so far
                bars = bars + resample(self.m[i0:last_closed_1m], tf)
            out[tf] = bars[-SNAPSHOT_BARS:]
        return out


# --- paper-trade replay -----------------------------------------------------------------------

@dataclass
class Trade:
    opened: int
    setup: Setup
    fill: float
    target: float = 0.0  # take-profit: target 1, or fill +/- N x risk with a fixed R:R exit
    stop: float = 0.0  # live stop; starts at the setup's stop and only moves in the trade's favour
    planned_r: float = 0.0
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


def auto_target_r(total: int) -> float:
    """Market-driven R:R: the stronger the trend total, the further the target (1:3 up to 1:6)."""
    return float(min(6, max(3, abs(total))))


def replay(candles_1m: list[Candle], levels: list[Level], symbol: str = "BTCUSD", exec_tf: str = "5m",
           cost_pct: float = 0.10, warmup_minutes: Optional[int] = None,
           reads: Optional[list] = None, target_r=None, stop_atr: float = BUFFER_ATR,
           max_candles: int = STALE_EXEC_CANDLES, trail: bool = False) -> tuple[list[Trade], list[str]]:
    """Step through history one exec candle at a time and paper-trade Laya's own rules.

    Pass a list as `reads` to collect (time_ms, bias, action) for every exec-candle read.
    target_r: exit at a fixed reward:risk (e.g. 4 for 1:4) instead of target 1, or "auto" to
        pick it per trade from the trend total at entry (auto_target_r).
    trail: once price has moved 1R in the trade's favour, move the stop to break-even and then
        keep it 1R behind the best price since entry.
    stop_atr: entry/stop distance from the level, in exec-timeframe ATRs.
    max_candles: close a trade still open after this many exec candles.

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
    snaps = Snapshotter(candles_1m)
    now = start
    while now <= end:
        snap = snaps.at(now)
        exec_c = [c for c in snap[exec_tf] if c.is_closed(now)]
        if open_trade is not None:
            _manage(open_trade, by_time, now - step, now, step, max_candles, trail)
            if open_trade.exit is not None:
                trades.append(open_trade)
                log.append(_trade_line(open_trade, cost_pct))
                open_trade = None
        if open_trade is None and pending is not None and len(exec_c) >= 2:
            open_trade = try_entry(pending, exec_c[-2], exec_c[-1], now, target_r)
        pending = analyse(build_analysis(symbol, snap, levels, now, exec_tf, cost_pct, "csv replay", stop_atr))
        if reads is not None and now < end:  # the read at `end` belongs to the next day
            reads.append((now, pending.verdict.bias, pending.verdict.action))
        now += step
    if open_trade is not None:
        last = candles_1m[-1]
        open_trade.exit, open_trade.closed, open_trade.reason = last.close, last.close_time, "end of data"
        trades.append(open_trade)
        log.append(_trade_line(open_trade, cost_pct))
    return trades, log


def try_entry(pending: Result, prev: Candle, cur: Candle, now: int, target_r=None) -> Optional[Trade]:
    """Open a paper trade if the read made at the previous close says ENTER and its trigger fired on `cur`.

    Takes the first valid, with-trend setup (in rank order) whose planned R:R is at least 1:1.5 and
    whose close-only trigger fired. Fill is cur's close.
    """
    v = pending.verdict
    if v.bias == "mixed":
        return None
    for r in pending.setups:
        # With a fixed R:R exit the planned reward is target_r, not the distance to target 1.
        planned = auto_target_r(v.total) if target_r == "auto" else target_r
        worth_it = planned >= WEAK_RR if planned else not r.weak
        if not (r.valid and r.alignment == "with-trend" and worth_it and trigger_fired(r.setup, prev, cur)):
            continue
        sign = 1 if r.direction == "long" else -1
        if sign * (cur.close - r.setup.stop) <= 0:
            continue  # the close is already through the stop; no trade
        target = r.setup.target1
        if planned:
            target = cur.close + sign * planned * abs(cur.close - r.setup.stop)
        if sign * (target - cur.close) <= 0:
            continue  # the close already ran past target 1
        return Trade(opened=now, setup=r.setup, fill=cur.close, target=target, stop=r.setup.stop,
                     planned_r=abs(target - cur.close) / abs(cur.close - r.setup.stop))
    return None


def _manage(t: Trade, by_time: dict[int, Candle], frm: int, to: int, step: int,
            max_candles: int = STALE_EXEC_CANDLES, trail: bool = False) -> None:
    long = t.setup.direction == "long"
    if not t.stop:
        t.stop = t.setup.stop
    ms = frm
    while ms < to:
        c = by_time.get(ms)
        ms += 60_000
        if c is None or c.open_time < t.opened:
            continue
        hit_stop = c.low <= t.stop if long else c.high >= t.stop
        hit_target = c.high >= t.target if long else c.low <= t.target
        if hit_stop:  # checked first: inside one 1m candle the stop is assumed to come first
            moved = t.stop != t.setup.stop
            t.exit, t.closed, t.reason = t.stop, c.close_time, "trail stop" if moved else "stop"
            return
        if hit_target:
            t.exit, t.closed, t.reason = t.target, c.close_time, "target"
            return
        if trail:  # moves only after this candle, so it never stops out on the candle that set it
            if long and c.high - t.fill >= t.risk:
                t.stop = max(t.stop, c.high - t.risk)
            elif not long and t.fill - c.low >= t.risk:
                t.stop = min(t.stop, c.low + t.risk)
    if to - t.opened >= max_candles * step:
        last = by_time.get(to - 60_000)
        if last is not None:
            t.exit, t.closed, t.reason = last.close, to, f"time stop ({max_candles} candles)"


def _trade_line(t: Trade, cost_pct: float) -> str:
    s = t.setup
    return (f"{_ts(t.opened)} {s.direction:5} {s.kind:9} fill {fmt_price(t.fill)} stop {fmt_price(s.stop)} "
            f"1:{t.planned_r:.1f} "
            f"target {fmt_price(t.target)} -> {fmt_price(t.exit)} ({t.reason}) "
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


def daily_report(trades: list[Trade], reads: list, balance: float, cost_pct: float,
                 leverage: float = 1.0) -> list[str]:
    """One row per UTC day: the day's trend, the trades and the $ result; no-trade days say why."""
    days: dict[str, dict] = {}
    for ms, bias, action in reads:
        d = days.setdefault(_ts(ms)[:10], {"bull": 0, "bear": 0, "mixed": 0, "enter": 0, "trades": []})
        d[{"bullish": "bull", "bearish": "bear"}.get(bias, "mixed")] += 1
        d["enter"] += action == "ENTER-ON-TRIGGER"
    for t in trades:
        days.setdefault(_ts(t.opened)[:10], {"bull": 0, "bear": 0, "mixed": 0, "enter": 0, "trades": []})[
            "trades"].append(t)
    rows = ["| Day | Trend (share of 5m reads) | Trades | Won | Lost | P&L USD | Balance | Note |",
            "|-----|---------------------------|--------|-----|------|---------|---------|------|"]
    start = balance
    counts = {"trend": {"bullish": 0, "bearish": 0, "mixed": 0}, "no_trade": 0}
    for day in sorted(days):
        d = days[day]
        n = d["bull"] + d["bear"] + d["mixed"] or 1
        shares = {"bullish": d["bull"] / n, "bearish": d["bear"] / n, "mixed": d["mixed"] / n}
        trend = max(shares, key=shares.get)
        counts["trend"][trend] += 1
        pnl = won = lost = 0
        for t in d["trades"]:
            sign = 1 if t.setup.direction == "long" else -1
            p = balance * leverage * (sign * (t.exit - t.fill) / t.fill - cost_pct / 100)
            balance = max(0.0, balance + p)
            pnl += p
            won += p > 0
            lost += p <= 0
        if d["trades"]:
            note = ""
        else:
            counts["no_trade"] += 1
            if shares["mixed"] >= 0.99:
                note = "no trade: no trend all day (mixed)"
            elif d["enter"] == 0:
                note = "no trade: trend but no with-trend setup worth 1:1.5"
            else:
                note = "no trade: setups ready but no close trigger fired"
        mix = f"{trend} ({shares['bullish']:.0%} bull / {shares['bearish']:.0%} bear / {shares['mixed']:.0%} mixed)"
        rows.append(f"| {day} | {mix} | {len(d['trades'])} | {won} | {lost} | {pnl:+.4f} | ${balance:,.4f} | {note} |")
    total = len(trades)
    rows.append("")
    rows.append(f"Days: {len(days)} | trend days: {counts['trend']['bullish']} bullish, "
                f"{counts['trend']['bearish']} bearish, {counts['trend']['mixed']} no trend (mixed) | "
                f"no-trade days: {counts['no_trade']} | trades: {total}")
    rows.append(f"$ {start:,.2f} -> ${balance:,.4f} ({balance - start:+.4f} USD, "
                f"{(balance - start) / start * 100:+.2f}%)")
    return rows


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


# --- R:R sweep ---------------------------------------------------------------------------------

def breakeven_win_rate(r: float, cost_r: float = 0.0) -> float:
    """Win rate needed to break even at 1:r when each trade also pays cost_r (in R units).

    A win nets r - cost_r, a loss -1 - cost_r, so p*(r - c) = (1 - p)*(1 + c) -> p = (1 + c) / (r + 1).
    """
    return (1 + cost_r) / (r + 1)


def rr_stats(trades: list[Trade], r, cost_pct: float, start: float = 10.0, end_balance: float = 10.0) -> dict:
    wins = sum(t.reason == "target" for t in trades)
    losses = sum(t.reason == "stop" for t in trades)
    trailed = sum(t.reason == "trail stop" for t in trades)
    if r == "auto":
        r = sum(t.planned_r for t in trades) / len(trades) if trades else 4.5
    n = len(trades)
    stop_pct = sum(t.risk / t.fill for t in trades) / n * 100 if n else 0.0
    cost_r = cost_pct / stop_pct if stop_pct else 0.0
    gross = sum(t.r_multiple() for t in trades)
    net = sum(t.r_multiple(cost_pct) for t in trades)
    win_rate = wins / n if n else 0.0
    need = breakeven_win_rate(r, cost_r)
    change = (end_balance - start) / start
    if not n:
        verdict = f"SKIPPED (rules need >= 1:{WEAK_RR:g})" if r < WEAK_RR else "NO TRADES"
    else:
        verdict = "PROFIT" if change > 0.005 else "LOSS" if change < -0.005 else "BREAK EVEN"
    return dict(rr=r, trades=n, wins=wins, losses=losses, trailed=trailed,
                time_exits=n - wins - losses - trailed, win_rate=win_rate,
                breakeven=breakeven_win_rate(r), breakeven_after_cost=need, stop_pct=stop_pct,
                end_balance=end_balance,
                gross_r=gross, net_r=net, verdict=verdict)


def rr_table(rows: list[dict], cost_pct: float, start: float) -> list[str]:
    out = [f"| R:R | Trades | Target hits | Stopped | Trail exits | Time exits | Win rate | "
           f"Break-even (no cost) | Break-even ({cost_pct:.2f}% cost) | Result | ${start:g} -> |",
           "|-----|--------|-------------|---------|-------------|------------|----------|"
           "----------------------|-------------------------|--------|---------|"]
    for s in rows:
        label = s.get("label") or f"1:{s['rr']:g}"
        out.append(f"| {label} | {s['trades']} | {s['wins']} | {s['losses']} | {s['trailed']} | {s['time_exits']} | "
                   f"{s['win_rate']:.1%} | {s['breakeven']:.1%} | {s['breakeven_after_cost']:.1%} | "
                   f"{s['verdict']} | ${s['end_balance']:,.2f} |")
    return out


# --- live paper trading ------------------------------------------------------------------------

def paper_live(symbol: str, levels: list[Level], exec_tf: str, cost_pct: float, source: str,
               minutes: float, balance: float, target_r=None, stop_atr: float = BUFFER_ATR,
               max_candles: int = STALE_EXEC_CANDLES, trail: bool = False, out=None) -> list[Trade]:
    """Forward-test on live candles for `minutes`: read at every exec-candle close, paper-trade, log.

    Same decisions as replay(): entries via try_entry() on the read from the previous close,
    exits via _manage() on 1m candles. No orders are sent anywhere.
    """
    out = out or sys.stdout
    step = TF_MS[exec_tf]

    def say(line: str) -> None:
        print(line, file=out, flush=True)

    stop_at = time.time() + minutes * 60
    exit_rule = ("auto 1:3-1:6" if target_r == "auto" else f"1:{target_r:g}" if target_r else "target 1")
    say(f"LIVE PAPER TEST | {symbol} | {exec_tf} | exit {exit_rule} | stop {stop_atr:g} ATR | "
        f"time stop {max_candles} candles{' | trailing' if trail else ''} | ${balance:g} | "
        f"{minutes:g} min | levels {', '.join(f'{lv.price:g}' for lv in levels)}")
    pending: Optional[Result] = None
    open_trade: Optional[Trade] = None
    trades: list[Trade] = []
    while True:
        now = (int(time.time() * 1000) // step + 1) * step  # next exec-candle close
        if now / 1000 > stop_at:
            break
        time.sleep(max(0.0, now / 1000 - time.time()) + 5)  # let the exchange publish the closed candle
        candles = None
        for _ in range(6):
            try:
                candles, _name = fetch_all(symbol, source)
            except Exception as e:
                say(f"{_ts(now)} [data error] {e}")
                time.sleep(10)
                continue
            if any(c.open_time == now - step for c in candles[exec_tf]):
                break
            time.sleep(10)
        if candles is None:
            continue
        by_time = {c.open_time: c for c in candles["1m"] if c.is_closed(now)}
        exec_c = [c for c in candles[exec_tf] if c.is_closed(now)]
        if open_trade is not None:
            _manage(open_trade, by_time, now - step, now, step, max_candles, trail)
            if open_trade.exit is not None:
                trades.append(open_trade)
                say(f"{_ts(now)} CLOSED {_trade_line(open_trade, cost_pct)}")
                open_trade = None
        if open_trade is None and pending is not None and len(exec_c) >= 2:
            open_trade = try_entry(pending, exec_c[-2], exec_c[-1], now, target_r)
            if open_trade is not None:
                s = open_trade.setup
                say(f"{_ts(now)} OPENED paper {s.direction} {s.kind} fill {fmt_price(open_trade.fill)} "
                    f"stop {fmt_price(open_trade.stop)} target {fmt_price(open_trade.target)} "
                    f"(1:{open_trade.planned_r:.1f})")
        pending = analyse(build_analysis(symbol, candles, levels, now, exec_tf, cost_pct, "live", stop_atr))
        v = pending.verdict
        top = next((r for r in pending.setups if r.valid), None)
        top_txt = f"top #{top.setup.id} {top.direction} {top.setup.trigger_text}" if top else "no setup"
        events = level_events(exec_c[-2], exec_c[-1], levels) if len(exec_c) >= 2 else []
        state = "in trade" if open_trade else "flat"
        say(f"{_ts(now)} close {fmt_price(exec_c[-1].close) if exec_c else '-'} | {v.bias} {v.total:+d} "
            f"{v.action} ({v.confidence}) | {top_txt} | {state}" + (f" | {'; '.join(events)}" if events else ""))
    if open_trade is not None:  # mark to market at the last 1m close
        last = max(by_time.values(), key=lambda c: c.open_time) if by_time else None
        if last is not None:
            open_trade.exit, open_trade.closed, open_trade.reason = last.close, last.close_time, "end of test"
            trades.append(open_trade)
            say(f"CLOSED {_trade_line(open_trade, cost_pct)}")
    say(summarize(trades, cost_pct))
    for line in account(trades, balance, cost_pct):
        say(line)
    say("Study example, not financial advice.")
    return trades
