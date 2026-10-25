"""BTC watcher tests on real data: 2026-09-01 BTCUSD trades aggregated to 1m candles."""

import os
import tempfile
import unittest
from pathlib import Path

from laya_market import btc_watcher
from laya_market.engine import analyse
from laya_market.indicators import pivots, read_timeframe, score_structure, score_trend, sma
from laya_market.market_data import (
    Candle,
    coinbase_product,
    load_csv,
    parse_binance,
    parse_coinbase,
    resample,
    trades_to_candles,
)
from laya_market.models import Level
from laya_market.report import render

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "btcusd_2026-09-01_1m.csv"
LEVELS = [Level(p) for p in (76500, 77000, 77500, 78000, 78500, 79000)]  # test levels, not drawn lines
DAY = 1_788_220_800_000  # 2026-09-01T00:00:00Z
MIN = 60_000


def c(i, o, h, lo, cl, step=MIN, v=1.0):
    return Candle(DAY + i * step, o, h, lo, cl, v, step)


class MarketDataTest(unittest.TestCase):
    def test_trades_to_candles(self):
        trades = [(DAY + 1_000, 100.0, 1), (DAY + 30_000, 105.0, 2), (DAY + 59_000, 99.0, 1),
                  (DAY + 61_000, 101.0, 5)]
        out = trades_to_candles(trades)
        self.assertEqual(len(out), 2)
        first = out[0]
        self.assertEqual((first.open, first.high, first.low, first.close, first.volume), (100, 105, 99, 99, 4))
        self.assertEqual(out[1].open_time, DAY + MIN)

    def test_trades_csv(self):
        rows = ("product_symbol,price,size,timestamp,buyer_role\n"
                "BTCUSD,78557.0,5.0,2026-09-01 00:00:00.163651,maker\n"
                "BTCUSD,78560.0,1.0,2026-09-01 00:00:30.000000,taker\n"
                "BTCUSD,78550.0,2.0,2026-09-01 00:01:03.083956,taker\n")
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
            fh.write(rows)
        try:
            out = load_csv(fh.name)
        finally:
            os.unlink(fh.name)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].open_time, DAY)
        self.assertEqual((out[0].high, out[0].close, out[0].volume), (78560.0, 78560.0, 6.0))

    def test_fixture_loads(self):
        candles = load_csv(str(FIXTURE))
        self.assertEqual(len(candles), 1440)
        self.assertEqual(candles[0].open_time, DAY)
        self.assertEqual(candles[0].open, 78557.0)
        self.assertEqual(max(x.high for x in candles), 79204.0)
        self.assertEqual(min(x.low for x in candles), 76375.0)

    def test_resample_respects_now(self):
        candles = load_csv(str(FIXTURE))
        now = DAY + 7 * MIN + 20_000  # 00:07:20 -> 1m candles up to 00:06 are closed
        five = resample(candles, "5m", now)
        self.assertEqual(len(five), 2)
        self.assertFalse(five[-1].is_closed(now))
        self.assertEqual(five[0].high, max(x.high for x in candles[:5]))
        self.assertEqual(five[-1].close, candles[6].close)

    def test_parsers(self):
        b = parse_binance([[DAY, "1", "3", "0.5", "2", "10", DAY + MIN - 1, "0", 0, "0", "0", "0"]], "1m")
        self.assertEqual((b[0].open, b[0].high, b[0].low, b[0].close), (1, 3, 0.5, 2))
        cb = parse_coinbase([[DAY // 1000 + 60, 1, 4, 2, 3, 7], [DAY // 1000, 0.5, 3, 1, 2, 9]], "1m")
        self.assertEqual(cb[0].open_time, DAY)
        self.assertEqual((cb[0].open, cb[0].high, cb[0].low, cb[0].close), (1, 3, 0.5, 2))
        self.assertEqual(coinbase_product("BTCUSDT"), "BTC-USD")


class IndicatorTest(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(sma([1, 2, 3, 4], 2), 3.5)
        self.assertIsNone(sma([1], 2))

    def test_trend_score(self):
        self.assertEqual(score_trend(110, 105, 100), 2)
        self.assertEqual(score_trend(110, 100, 105), 1)
        self.assertEqual(score_trend(90, 95, 100), -2)
        self.assertEqual(score_trend(102, 105, 100), 0)

    def test_structure_uptrend(self):
        # zig-zag with higher highs and higher lows
        closes = [100, 104, 102, 106, 103, 108, 105, 110, 107, 112, 109]
        candles = [c(i, x, x + 1, x - 1, x) for i, x in enumerate(closes)]
        highs, lows = pivots(candles, side=1)
        self.assertGreaterEqual(len(highs), 2)
        self.assertEqual(score_structure([c(i, x, x + 1, x - 1, x) for i, x in enumerate(
            [100, 101, 104, 101, 100, 102, 106, 103, 102, 104, 108, 105, 104])]), 2)

    def test_read_timeframe_uses_closed_candles(self):
        candles = load_csv(str(FIXTURE))
        now = DAY + 18 * 3_600_000 + 32 * MIN + 20_000
        read = read_timeframe("5m", resample(candles, "5m", now), LEVELS, now)
        self.assertEqual(read.seconds_to_close, 160)
        self.assertIsNotNone(read.ma_slow)
        self.assertLess(read.ma_fast, read.ma_slow)  # the 18:00 sell-off
        self.assertEqual(read.trend, -2)


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self.candles = load_csv(str(FIXTURE))

    def test_setups_only_from_supplied_levels(self):
        setups = btc_watcher.build_setups(77200, LEVELS, 100, "5m")
        self.assertEqual({s.kind for s in setups}, {"breakout", "rejection", "breakdown", "bounce"})
        level_prices = {lv.price for lv in LEVELS}
        for s in setups:
            self.assertIn(s.trigger_level, level_prices)
            self.assertIn(s.target1, level_prices)
        self.assertEqual(btc_watcher.build_setups(77200, [], 100, "5m"), [])

    def test_trigger_detection(self):
        s = btc_watcher.build_setups(77200, LEVELS, 100, "5m")
        by = {x.kind: x for x in s}
        prev, cur = c(0, 77400, 77450, 77380, 77450, 300_000), c(1, 77450, 77600, 77440, 77560, 300_000)
        self.assertTrue(btc_watcher.trigger_fired(by["breakout"], prev, cur))
        wick = c(1, 77450, 77560, 77440, 77470, 300_000)
        self.assertTrue(btc_watcher.trigger_fired(by["rejection"], prev, wick))
        self.assertFalse(btc_watcher.trigger_fired(by["breakout"], prev, wick))
        self.assertIn("wicked above 77,500.00 and closed back below", btc_watcher.level_events(prev, wick, LEVELS))

    def test_snapshot_report(self):
        now = DAY + 18 * 3_600_000 + 32 * MIN + 20_000
        inp = btc_watcher.build_analysis("BTCUSD", btc_watcher.snapshot(self.candles, now), LEVELS, now)
        res = analyse(inp)
        self.assertEqual(res.verdict.bias, "bearish")
        self.assertEqual([t.read.timeframe for t in res.timeframes], ["1h", "15m", "5m", "1m"])
        self.assertTrue(2 <= len(res.setups) <= 4)
        self.assertEqual(res.setups[0].direction, "short")
        self.assertTrue(all(r.alignment == "counter-trend" for r in res.setups if r.direction == "long"))
        text = render(res)
        self.assertIn("5m candle closes in 2m 40s", text)
        self.assertNotIn("Corrections to the supplied numbers", text)

    def test_no_levels_asks_for_them(self):
        now = DAY + 12 * 3_600_000
        res = analyse(btc_watcher.build_analysis("BTCUSD", btc_watcher.snapshot(self.candles, now), [], now))
        self.assertEqual(res.setups, [])
        self.assertIn("levels", res.question)

    def test_replay_paper_trades(self):
        trades, log = btc_watcher.replay(self.candles, LEVELS, "BTCUSD", "5m", 0.10)
        self.assertEqual(len(trades), len(log))
        self.assertGreater(len(trades), 0)
        for t in trades:
            self.assertTrue(t.reason in ("stop", "target 1", "end of data") or t.reason.startswith("time stop"))
            self.assertGreaterEqual(t.r_multiple(), -1.0001)  # a stop can't lose more than 1R gross
            self.assertLess(t.r_multiple(0.10), t.r_multiple())
            self.assertGreater(t.closed, t.opened)
        self.assertIn("paper trades", btc_watcher.summarize(trades, 0.10))

    def test_ten_dollar_account(self):
        trades, _ = btc_watcher.replay(self.candles, LEVELS, "BTCUSD", "5m", 0.10)
        lines = btc_watcher.account(trades, 10.0, 0.10)
        self.assertEqual(len(lines), len(trades) + 2)
        # first trade: long time-stopped 78,705 -> 78,658 = -0.0597% move - 0.10% cost on $10
        first = trades[0]
        expected = 10 * ((first.exit - first.fill) / first.fill - 0.001)
        self.assertIn(f"P&L {expected:+.4f} USD", lines[1])
        self.assertTrue(lines[-1].startswith("End balance $9.9441"))

    def test_account_with_no_trades(self):
        self.assertIn("End balance $10.0000", btc_watcher.account([], 10.0, 0.1)[-1])


if __name__ == "__main__":
    unittest.main()
