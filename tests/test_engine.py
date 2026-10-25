import json
import unittest
from pathlib import Path

from laya_market.engine import analyse, bias_from_total, evaluate_setup
from laya_market.models import AnalysisInput, InputError, Setup
from laya_market.report import render, to_dict

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "examples" / "sample_analysis.json"


def sample():
    return AnalysisInput.from_dict(json.loads(SAMPLE.read_text()))


class SampleAnalysisTest(unittest.TestCase):
    """The user-supplied hypothetical read, recomputed by the rules."""

    def setUp(self):
        self.res = analyse(sample())
        self.by_id = {r.setup.id: r for r in self.res.setups}

    def test_scores_and_bias(self):
        scores = {t.read.timeframe: t.read.score for t in self.res.timeframes}
        self.assertEqual(scores, {"1h": 2, "15m": -2, "5m": 1, "1m": 2})
        self.assertEqual(self.res.verdict.total, 3)
        self.assertEqual(self.res.verdict.bias, "bullish")

    def test_top_down_order_and_counter_trend(self):
        self.assertEqual([t.read.timeframe for t in self.res.timeframes], ["1h", "15m", "5m", "1m"])
        counter = [t.read.timeframe for t in self.res.timeframes if t.counter_trend]
        self.assertEqual(counter, ["15m"])

    def test_rr_from_actual_numbers(self):
        s1, s2, s3 = self.by_id[1], self.by_id[2], self.by_id[3]
        self.assertAlmostEqual(s1.rr1, 0.0040 / 0.0035, places=6)
        self.assertAlmostEqual(s1.rr2, 0.0090 / 0.0035, places=6)
        self.assertAlmostEqual(s2.rr1, 1.0, places=6)
        self.assertAlmostEqual(s2.rr2, 2.25, places=6)
        self.assertAlmostEqual(s3.rr1, 1.0, places=6)
        self.assertAlmostEqual(s3.rr2, 0.008 / 0.003, places=6)
        self.assertAlmostEqual(s1.stop_pct, 0.0035 / 1.2060 * 100, places=6)
        self.assertTrue(all(r.weak for r in self.res.setups))

    def test_counter_trend_setup_ranked_last(self):
        self.assertEqual(self.res.setups[-1].setup.id, 2)
        self.assertEqual(self.by_id[2].alignment, "counter-trend")
        self.assertEqual({self.by_id[1].alignment, self.by_id[3].alignment}, {"with-trend"})

    def test_weak_top_setup_means_wait(self):
        self.assertEqual(self.res.verdict.action, "WAIT")
        self.assertEqual(self.res.verdict.confidence, "moderate")

    def test_audit_flags_wrong_claims(self):
        audit = "\n".join(self.res.audit)
        self.assertIn("Setup 1: claimed R:R 5", audit)
        self.assertIn("Setup 3: claimed R:R 20", audit)
        self.assertIn("claimed mixed", audit)

    def test_invalidation_per_timeframe(self):
        inv = self.res.invalidation
        self.assertIn("1h: a close below 1.2000 cancels the bullish lean", inv)
        self.assertIn("15m: a close above 1.2050 cancels the bearish lean", inv)

    def test_timing(self):
        self.assertTrue(any("5m candle closes in 45s" in line for line in self.res.timing))
        self.assertTrue(any("stop 0.29%" in line for line in self.res.timing))

    def test_report_format(self):
        text = render(self.res)
        for heading in ("1. Verdict", "2. Timeframe alignment", "3. Setups", "4. Ranking",
                        "5. Invalidation", "6. Timing and costs", "7. Blind spots"):
            self.assertIn(heading, text)
        self.assertEqual(text.lower().count("not financial advice"), 1)
        self.assertTrue(text.rstrip().endswith("Study example, not financial advice."))
        self.assertIsNone(self.res.question)
        json.dumps(to_dict(self.res))


class RuleTest(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(bias_from_total(3), "bullish")
        self.assertEqual(bias_from_total(-3), "bearish")
        self.assertEqual(bias_from_total(2), "mixed")
        self.assertEqual(bias_from_total(-2), "mixed")

    def test_stop_must_sit_beyond_trigger(self):
        s = Setup(id=1, trigger_side="above", trigger_level=100, entry=101, stop=100.5, target1=104)
        r = evaluate_setup(s, [], 0.0)
        self.assertFalse(r.valid)
        self.assertIn("beyond", r.errors[0])

    def test_target_on_wrong_side(self):
        s = Setup(id=1, trigger_side="below", trigger_level=100, entry=99, stop=101, target1=99.5 + 1)
        self.assertFalse(evaluate_setup(s, [], 0.0).valid)

    def test_costs_reduce_rr(self):
        s = Setup(id=1, trigger_side="above", trigger_level=100, entry=100.2, stop=99.8, target1=101)
        r = evaluate_setup(s, [], 0.1)
        self.assertAlmostEqual(r.rr1, 2.0)
        self.assertLess(r.net_rr1, r.rr1)

    def test_bad_component_rejected(self):
        with self.assertRaises(InputError):
            AnalysisInput.from_dict({"timeframes": [{"timeframe": "1h", "trend": 3}]})

    def test_one_question_when_timeframe_missing(self):
        res = analyse(AnalysisInput.from_dict({"symbol": "X", "timeframes": [{"timeframe": "5m"}]}))
        self.assertIn("1h", res.question)
        self.assertTrue(any("Missing timeframes" in b for b in res.blind_spots))


if __name__ == "__main__":
    unittest.main()
