import hashlib
import hmac
import os
import tempfile
import unittest

from laya_market import delta


class DeltaTest(unittest.TestCase):
    def test_signature(self):
        got = delta.sign("secret", "GET", "1700000000", "/v2/wallet/balances", "?a=1")
        want = hmac.new(b"secret", b"GET1700000000/v2/wallet/balances?a=1", hashlib.sha256).hexdigest()
        self.assertEqual(got, want)

    def test_parse_candles_sorted(self):
        rows = [{"time": 120, "open": 2, "high": 3, "low": 1, "close": 2.5, "volume": 7},
                {"time": 60, "open": 1, "high": 2, "low": 0.5, "close": 2, "volume": 5}]
        out = delta.parse_candles(rows, "1m")
        self.assertEqual([c.open_time for c in out], [60_000, 120_000])
        self.assertEqual(out[1].close, 2.5)

    def test_load_env_does_not_override(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write("# comment\nLAYA_TEST_X=from_file\nLAYA_TEST_Y='quoted'\n")
        try:
            os.environ["LAYA_TEST_X"] = "already"
            delta.load_env(fh.name)
            self.assertEqual(os.environ["LAYA_TEST_X"], "already")
            self.assertEqual(os.environ["LAYA_TEST_Y"], "quoted")
        finally:
            os.unlink(fh.name)
            os.environ.pop("LAYA_TEST_X", None)
            os.environ.pop("LAYA_TEST_Y", None)


if __name__ == "__main__":
    unittest.main()
