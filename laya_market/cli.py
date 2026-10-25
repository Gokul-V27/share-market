"""Command line: analyze a JSON read, watch BTC (live or CSV), or analyse screenshots."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from . import btc_watcher, text_guard
from .engine import analyse
from .market_data import DataError, load_csv, write_candles_csv
from .models import AnalysisInput, InputError
from .report import render, render_json


def _apply_guard(inp: AnalysisInput, note: str, backend: str) -> None:
    if not note:
        return
    g = text_guard.check(note, backend)
    if g.asks_for_call:
        print(text_guard.REFUSAL + "\n")
    if g.mentions_news:
        inp.blind_spots.insert(0, f"Your note mentions scheduled high-impact news (p={g.news:.2f}, "
                                  f"{g.backend}); levels may not hold through it")


def cmd_analyze(a) -> int:
    with open(a.file) as fh:
        inp = AnalysisInput.from_dict(json.load(fh))
    _apply_guard(inp, a.note, a.guard)
    res = analyse(inp)
    print(render_json(res) if a.json else render(res))
    return 0


def _parse_at(value: str) -> int:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def cmd_btc(a) -> int:
    levels = btc_watcher.load_levels(a.levels_file, a.level)
    if a.csv:
        candles = load_csv(a.csv)
        if not candles:
            raise DataError(f"{a.csv}: no candles")
        if a.save_1m:
            write_candles_csv(candles, a.save_1m)
            print(f"wrote {len(candles)} 1m candles to {a.save_1m}", file=sys.stderr)
        if a.replay:
            reads: list = []
            trades, log = btc_watcher.replay(candles, levels, a.symbol, a.exec_tf, a.cost_pct, reads=reads,
                                             target_r=a.target_r, stop_atr=a.stop_atr,
                                             max_candles=a.max_candles, trail=a.trail)
            exit_rule = ("auto 1:3-1:6 from trend strength" if a.target_r == "auto" else
                         f"fixed 1:{a.target_r}" if a.target_r else "target 1 (next drawn level)")
            if a.trail:
                exit_rule += ", trailing stop after 1R"
            print(f"Paper-trade replay of {a.csv} ({len(candles)} 1m candles, levels: "
                  f"{', '.join(f'{lv.price:g}' for lv in levels) or 'none'})")
            print(f"Exit: {exit_rule} | stop {a.stop_atr:g} ATR beyond the level | "
                  f"time stop {a.max_candles} {a.exec_tf} candles")
            print("\n".join(log) if log else "(no trades)")
            print(btc_watcher.summarize(trades, a.cost_pct))
            if a.balance:
                print()
                print("\n".join(btc_watcher.account(trades, a.balance, a.cost_pct, a.leverage)))
                print()
                print("\n".join(btc_watcher.daily_report(trades, reads, a.balance, a.cost_pct, a.leverage)))
            print("Study example, not financial advice.")
            return 0
        now = _parse_at(a.at) if a.at else candles[-1].close_time
        snap = btc_watcher.snapshot(candles, now)
        inp = btc_watcher.build_analysis(a.symbol, snap, levels, now, a.exec_tf, a.cost_pct, f"csv {a.csv}")
        _apply_guard(inp, a.note, a.guard)
        res = analyse(inp)
        print(render_json(res) if a.json else render(res))
        return 0
    if a.note:
        g = text_guard.check(a.note, a.guard)
        if g.asks_for_call:
            print(text_guard.REFUSAL + "\n")
    return btc_watcher.watch(a.symbol, levels, a.exec_tf, a.cost_pct, a.source, a.every, not a.watch, a.json)


def cmd_screenshot(a) -> int:
    from .vision import analyse_screenshots

    if a.note and text_guard.check(a.note, a.guard).asks_for_call:
        print(text_guard.REFUSAL + "\n")
    text, read = analyse_screenshots(a.images, a.note, a.model)
    print(text)
    if read:
        try:
            res = analyse(AnalysisInput.from_dict(read))
        except (InputError, KeyError, TypeError, ValueError) as e:
            print(f"\n[engine re-check skipped: {e}]")
            return 0
        if res.audit:
            print("\nEngine re-check of the numbers read from the screenshots:")
            print("\n".join(f"   - {line}" for line in res.audit))
        else:
            print("\nEngine re-check: scores, R:R and verdict match the rules.")
        if a.save:
            with open(a.save, "w") as fh:
                json.dump(read, fh, indent=2)
            print(f"saved the read to {a.save}; re-run with: python -m laya_market analyze {a.save}")
    return 0


def cmd_download(a) -> int:
    from .market_data import fetch_history

    start = _parse_at(a.start)
    end = start + a.days * 86_400_000
    candles = fetch_history(a.symbol, start, end,
                            progress=lambda n: print(f"\r{n} candles", end="", file=sys.stderr))
    print(file=sys.stderr)
    write_candles_csv(candles, a.out)
    print(f"wrote {len(candles)} 1m candles ({a.days} days of {a.symbol}) to {a.out}")
    return 0


def cmd_rr(a) -> int:
    from .models import Level

    rr_list = [x.strip() if x.strip() == "auto" else float(x) for x in a.rr.split(",")]
    per_rr: dict = {r: [] for r in rr_list}
    balances = {r: a.balance for r in rr_list}
    for path in a.csv:
        candles = load_csv(path)
        if a.levels_file or a.level:
            levels = btc_watcher.load_levels(a.levels_file, a.level)
        else:  # test levels: every --levels-step across this file's range
            lo, hi = min(c.low for c in candles), max(c.high for c in candles)
            step = a.levels_step
            levels = [Level(p, "test level") for p in range(int(lo // step) * step, int(hi // step + 1) * step + 1, step)]
        for r in rr_list:
            trades, _ = btc_watcher.replay(candles, levels, a.symbol, a.exec_tf, a.cost_pct, target_r=r,
                                           stop_atr=a.stop_atr, max_candles=a.max_candles, trail=a.trail)
            per_rr[r].extend(trades)
            for line in btc_watcher.account(trades, balances[r], a.cost_pct)[-1:]:
                balances[r] = float(line.split("$")[1].split(" ")[0].replace(",", ""))
            print(f"{path}: {r} done ({len(trades)} trades)", file=sys.stderr)
    rows = []
    for r in rr_list:
        row = btc_watcher.rr_stats(per_rr[r], r, a.cost_pct, a.balance, balances[r])
        if r == "auto":
            row["label"] = f"auto (avg 1:{row['rr']:.1f})"
        rows.append(row)
    trail = " | trailing stop" if a.trail else ""
    print(f"R:R sweep | {a.exec_tf} | stop {a.stop_atr:g} ATR beyond the level | time stop {a.max_candles} candles"
          f"{trail} | {len(a.csv)} file(s)")
    print("\n".join(btc_watcher.rr_table(rows, a.cost_pct, a.balance)))
    print("Break-even win rate = (1 + cost in R) / (1 + R). Trail and time exits are neither full wins nor "
          "full losses. auto = 1:3 to 1:6 from the trend total at entry. $ = full balance per trade, compounding.")
    print("Study example, not financial advice.")
    return 0


def cmd_delta_account(a) -> int:
    from . import delta

    bal = [b for b in delta.wallet_balances() if float(b.get("balance") or 0)]
    pos = [p for p in delta.positions() if float(p.get("size") or 0)]
    print("Delta Exchange account (read-only)")
    if not bal:
        print("  no non-zero balances")
    for b in bal:
        print(f"  {b.get('asset_symbol')}: balance {b.get('balance')} | available {b.get('available_balance')}")
    print("Open positions:" if pos else "No open positions.")
    for p in pos:
        print(f"  {p.get('product_symbol')}: size {p.get('size')} @ {p.get('entry_price')} | "
              f"liq {p.get('liquidation_price')} | unrealised P&L {p.get('unrealized_pnl')}")
    return 0


def cmd_guard(a) -> int:
    g = text_guard.check(a.text, a.guard)
    print(json.dumps({"asks_for_guaranteed_call": g.asks_for_call, "p_guarantee": round(g.guarantee, 3),
                      "mentions_scheduled_news": g.mentions_news, "p_news": round(g.news, 3),
                      "backend": g.backend}, indent=2))
    return 0


def _target_r(value: str):
    return "auto" if value == "auto" else float(value)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="laya_market", description="Laya: rule-based chart reads for study.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def guard_opts(sp):
        sp.add_argument("--note", default="", help="free-text note/question; checked by the text guard")
        sp.add_argument("--guard", choices=("auto", "laya", "keywords"), default="auto",
                        help="text guard backend (laya model if installed, else keywords)")

    an = sub.add_parser("analyze", help="score a JSON chart read (see examples/sample_analysis.json)")
    an.add_argument("file")
    an.add_argument("--json", action="store_true")
    guard_opts(an)
    an.set_defaults(func=cmd_analyze)

    b = sub.add_parser("btc", help="BTC watcher: live candles, a CSV snapshot, or a paper-trade replay")
    b.add_argument("--symbol", default="BTCUSDT")
    b.add_argument("--level", type=float, action="append", default=[], help="a drawn level; repeat")
    b.add_argument("--levels-file", help='JSON: {"levels": [{"price": 78000, "label": "..."}]}')
    b.add_argument("--exec-tf", default="5m", choices=("1m", "5m", "15m"))
    b.add_argument("--cost-pct", type=float, default=0.10, help="assumed round-trip fees+spread+slippage, %%")
    b.add_argument("--source", default="auto", choices=("auto", "binance", "coinbase", "delta"),
                   help="delta = Delta Exchange India (use --symbol BTCUSD)")
    b.add_argument("--watch", action="store_true", help="keep refreshing")
    b.add_argument("--every", type=float, default=30, help="refresh seconds with --watch")
    b.add_argument("--csv", help="trades CSV (price,size,timestamp) or OHLCV CSV instead of live data")
    b.add_argument("--at", help="with --csv: analyse as of this UTC time, e.g. 2026-09-01T14:00")
    b.add_argument("--replay", action="store_true", help="with --csv: paper-trade the rules over the file")
    b.add_argument("--save-1m", help="with --csv: also write the aggregated 1m candles here")
    b.add_argument("--balance", type=float, help="with --replay: paper account size in USD, e.g. 10")
    b.add_argument("--leverage", type=float, default=1.0, help="with --balance: position = balance x this")
    b.add_argument("--target-r", type=_target_r, help="with --replay: exit at a fixed R:R, e.g. 4 for 1:4, or auto")
    b.add_argument("--trail", action="store_true", help="with --replay: trail the stop once price moves 1R")
    b.add_argument("--stop-atr", type=float, default=btc_watcher.BUFFER_ATR,
                   help="entry/stop distance from the level in exec-timeframe ATRs (default 0.5)")
    b.add_argument("--max-candles", type=int, default=btc_watcher.STALE_EXEC_CANDLES,
                   help="with --replay: close trades still open after this many exec candles (default 8)")
    b.add_argument("--json", action="store_true")
    guard_opts(b)
    b.set_defaults(func=cmd_btc)

    s = sub.add_parser("screenshot", help="analyse chart screenshots with Claude, then re-check the numbers")
    s.add_argument("images", nargs="+")
    s.add_argument("--model", default="claude-opus-5-5")
    s.add_argument("--save", help="write the JSON read here for later re-analysis")
    guard_opts(s)
    s.set_defaults(func=cmd_screenshot)

    d = sub.add_parser("download", help="save historical 1m candles from Binance to a CSV (.csv or .csv.gz)")
    d.add_argument("--symbol", default="BTCUSDT")
    d.add_argument("--start", required=True, help="UTC start, e.g. 2026-09-01")
    d.add_argument("--days", type=int, default=30)
    d.add_argument("--out", required=True)
    d.set_defaults(func=cmd_download)

    rr = sub.add_parser("rr", help="replay CSV(s) at several fixed R:R exits and compare win rate vs break-even")
    rr.add_argument("--csv", action="append", required=True, help="1m candle or trades CSV; repeat for more months")
    rr.add_argument("--rr", default="1,2,3,4,5,6,auto", help="comma list of reward multiples, or auto")
    rr.add_argument("--trail", action="store_true", help="trail the stop once price moves 1R in favour")
    rr.add_argument("--symbol", default="BTCUSDT")
    rr.add_argument("--exec-tf", default="15m", choices=("1m", "5m", "15m"))
    rr.add_argument("--stop-atr", type=float, default=1.5)
    rr.add_argument("--max-candles", type=int, default=96)
    rr.add_argument("--cost-pct", type=float, default=0.10)
    rr.add_argument("--balance", type=float, default=10.0)
    rr.add_argument("--level", type=float, action="append", default=[])
    rr.add_argument("--levels-file")
    rr.add_argument("--levels-step", type=int, default=500, help="test levels every N USD when none are given")
    rr.set_defaults(func=cmd_rr)

    da = sub.add_parser("delta-account", help="read-only Delta Exchange balances and positions (keys in .env)")
    da.set_defaults(func=cmd_delta_account)

    g = sub.add_parser("guard", help="run the text guard on a sentence")
    g.add_argument("text")
    g.add_argument("--guard", choices=("auto", "laya", "keywords"), default="auto")
    g.set_defaults(func=cmd_guard)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.func(a)
    except (InputError, DataError, OSError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        from .vision import VisionError

        if isinstance(e, VisionError):
            print(f"error: {e}", file=sys.stderr)
            return 2
        raise
