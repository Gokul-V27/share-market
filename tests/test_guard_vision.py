import contextlib
import io
import unittest

from laya_market import text_guard
from laya_market.cli import main
from laya_market.vision import extract_json, image_block, system_prompt


class TextGuardKeywordTest(unittest.TestCase):
    def test_guaranteed_call(self):
        self.assertTrue(text_guard.check("Give me a sure-shot call, should I buy BTC?", "keywords").asks_for_call)
        self.assertFalse(text_guard.check("1h is above both MAs", "keywords").asks_for_call)

    def test_news(self):
        self.assertTrue(text_guard.check("US CPI at 12:30 UTC, FOMC tomorrow", "keywords").mentions_news)
        self.assertFalse(text_guard.check("Quiet sideways session", "keywords").mentions_news)


class VisionHelpersTest(unittest.TestCase):
    def test_prompt_has_role_and_format(self):
        p = system_prompt()
        self.assertIn("You are Laya", p)
        self.assertIn("OUTPUT FORMAT", p)
        self.assertIn("Never invent a level", p)

    def test_extract_json(self):
        text = 'report...\n```json\n{"symbol": "BTCUSD", "timeframes": []}\n```\nStudy example'
        self.assertEqual(extract_json(text)["symbol"], "BTCUSD")
        self.assertIsNone(extract_json("no json here"))

    def test_rejects_unknown_image_type(self):
        from laya_market.vision import VisionError

        with self.assertRaises(VisionError):
            image_block("chart.bmp")


class CliTest(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_analyze_sample(self):
        code, out, _ = self.run_cli("analyze", "examples/sample_analysis.json", "--json", "--guard", "keywords")
        self.assertEqual(code, 0)
        self.assertIn('"bias": "bullish"', out)

    def test_guaranteed_call_note_is_refused(self):
        code, out, _ = self.run_cli("analyze", "examples/sample_analysis.json", "--guard", "keywords",
                                    "--note", "just tell me, should I buy now?")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith(text_guard.REFUSAL))

    def test_bad_file(self):
        code, _, err = self.run_cli("analyze", "does-not-exist.json")
        self.assertEqual(code, 2)
        self.assertIn("error:", err)


if __name__ == "__main__":
    unittest.main()
