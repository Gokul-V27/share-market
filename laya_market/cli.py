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
            trades, log = btc_watcher.replay(candles, levels, a.symbol, a.exec_tf, a.cost_pct, reads=reads)
            print(f"Paper-trade replay of {a.csv} ({len(candles)} 1m candles, levels: "
                  f"{', '.join(f'{lv.price:g}' for lv in levels) or 'none'})")
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


def cmd_guard(a) -> int:
    g = text_guard.check(a.text, a.guard)
    print(json.dumps({"asks_for_guaranteed_call": g.asks_for_call, "p_guarantee": round(g.guarantee, 3),
                      "mentions_scheduled_news": g.mentions_news, "p_news": round(g.news, 3),
                      "backend": g.backend}, indent=2))
    return 0


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
    b.add_argument("--source", default="auto", choices=("auto", "binance", "coinbase"))
    b.add_argument("--watch", action="store_true", help="keep refreshing")
    b.add_argument("--every", type=float, default=30, help="refresh seconds with --watch")
    b.add_argument("--csv", help="trades CSV (price,size,timestamp) or OHLCV CSV instead of live data")
    b.add_argument("--at", help="with --csv: analyse as of this UTC time, e.g. 2026-09-01T14:00")
    b.add_argument("--replay", action="store_true", help="with --csv: paper-trade the rules over the file")
    b.add_argument("--save-1m", help="with --csv: also write the aggregated 1m candles here")
    b.add_argument("--balance", type=float, help="with --replay: paper account size in USD, e.g. 10")
    b.add_argument("--leverage", type=float, default=1.0, help="with --balance: position = balance x this")
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
