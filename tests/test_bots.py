import unittest

from laya_market import bots, funnel
from laya_market.btc_watcher import Trade
from laya_market.market_data import Candle
from laya_market.models import Setup

DAY = 1_788_220_800_000  # 2026-09-01T00:00:00Z
MIN = 60_000


def series(prices, start=DAY):
    """1m candles moving through the given closes (high/low 1 either side)."""
    out, prev = [], prices[0]
    for i, p in enumerate(prices):
        out.append(Candle(start + i * MIN, prev, max(prev, p) + 1, min(prev, p) - 1, p, 1.0, MIN))
        prev = p
    return out


class OrbTest(unittest.TestCase):
    def test_breakout_long_then_stop(self):
        # 30 min range 100-110, breakout to 120, then collapse below the range low
        prices = [105 + (5 if i % 2 else -5) for i in range(30)] + [112] * 10 + [120] * 20 + [90] * 20
        trades = bots.orb_bot(series(prices), 30)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t.setup.direction, "long")
        self.assertEqual(t.stop, 99)  # range low incl. the 1-point wick
        self.assertEqual(t.reason, "stop")

    def test_no_trade_without_breakout(self):
        prices = [105 + (5 if i % 2 else -5) for i in range(30)] + [105] * 60
        self.assertEqual(bots.orb_bot(series(prices), 30), [])


class SweepTest(unittest.TestCase):
    def test_sweep_low_close_back_inside_long_hits_target(self):
        asia = [100 + (i % 2) * 100 for i in range(8 * 60)]  # range ~ 99 .. 201
        after = [150] * 10 + [90] * 5 + [110] * 10 + [140] * 10 + [200] * 10
        trades = bots.sweep_bot(series(asia + after))
        self.assertEqual(len(trades), 1)
        t = trades[0]
        size = 201 - 99
        self.assertEqual(t.setup.direction, "long")
        self.assertAlmostEqual(t.stop, 99 + 0.125 * size)
        self.assertAlmostEqual(t.target, 99 + 0.5 * size)
        self.assertEqual(t.reason, "target")
        self.assertAlmostEqual(t.planned_r, 2.0, places=6)


class FunnelTest(unittest.TestCase):
    def _trade(self, fill, stop, exit_, when=DAY):
        s = Setup(id=1, trigger_side="above", trigger_level=stop, entry=fill, stop=stop, target1=fill + 1, kind="t")
        return Trade(opened=when, setup=s, fill=fill, stop=stop, exit=exit_, reason="x")

    def test_risk_gate(self):
        tight = self._trade(80000, 79990, 79990)  # 0.0125% stop: fees ~8R
        wide = self._trade(80000, 79600, 79600)  # 0.5% stop
        ds = funnel.run([tight, wide], [], [], check="none", cost_pct=0.10, snaps=object())
        self.assertEqual([d.stage for d in ds], ["risk", "broker"])
        self.assertIn("fees", ds[0].reason)

    def test_daily_loss_limit(self):
        losers = [self._trade(80000, 79600, 79600, DAY + i * MIN) for i in range(3)]
        ds = funnel.run(losers, [], [], check="none", cost_pct=0.10, snaps=object())
        self.assertEqual([d.taken for d in ds], [True, True, False])


if __name__ == "__main__":
    unittest.main()
